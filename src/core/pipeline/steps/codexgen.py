from __future__ import annotations

import json
import time
from typing import Any, Callable

from src.config.constants import OPENAI_API_ENDPOINTS, OPENAI_PAGE_TYPES, generate_random_user_info
from src.core.pipeline.context import PipelineContext
from src.core.pipeline.definitions import PipelineDefinition, StepDefinition
from src.core.pipeline.registry import PIPELINE_REGISTRY, register_pipeline
from src.core.register import RegistrationEngine
from src.services import BaseEmailService

from .common import (
    create_email_step,
    exchange_oauth_token_step,
    get_proxy_ip_step,
    get_registration_engine,
    persist_account_step,
    schedule_survival_checks_step,
    wait_login_otp_step,
    wait_signup_otp_step,
)


class CodexgenPipelineRuntime:
    """
    Codexgen pipeline runtime adapter.

    保留 codexgen 的关键链路意图（sentinel、create_account fallback、consent/workspace/token），
    同时统一复用当前项目 email service 与 HTTP/OAuth 基础设施。
    """

    def __init__(
        self,
        *,
        email_service: BaseEmailService,
        proxy_url: str | None = None,
        callback_logger: Callable[[str], None] | None = None,
        task_uuid: str | None = None,
    ) -> None:
        self._engine = RegistrationEngine(
            email_service=email_service,
            proxy_url=proxy_url,
            callback_logger=callback_logger,
            task_uuid=task_uuid,
        )
        self._signup_otp_code: str | None = None
        self._login_otp_code: str | None = None

    @property
    def email(self) -> str | None:
        return self._engine.email

    @property
    def password(self) -> str | None:
        return self._engine.password

    @property
    def email_info(self) -> dict[str, Any] | None:
        return self._engine.email_info

    def run_check_ip_location_step(self) -> dict[str, Any]:
        ip_ok, location = self._engine._check_ip_location()  # noqa: SLF001
        if not ip_ok:
            raise RuntimeError(f"ip location unsupported: {location}")
        return {"metadata": {"ip_location": location}}

    def run_create_email_step(self) -> dict[str, Any]:
        if not self._engine._create_email():  # noqa: SLF001
            raise RuntimeError("create_email failed")
        return {
            "email": self._engine.email,
            "metadata": {"email_info": self._engine.email_info or {}},
        }

    def run_init_auth_session_step(self) -> dict[str, Any]:
        if not self._engine._init_session():  # noqa: SLF001
            raise RuntimeError("init auth session failed")
        return {}

    def run_prepare_authorize_flow_step(self) -> dict[str, Any]:
        if not self._engine.session and not self._engine._init_session():  # noqa: SLF001
            raise RuntimeError("init auth session failed")
        if not self._engine._start_oauth():  # noqa: SLF001
            raise RuntimeError("start oauth failed")

        did = self._engine._get_device_id()  # noqa: SLF001
        if not did:
            raise RuntimeError("get device id failed")

        sentinel_token = self._engine._check_sentinel(did)  # noqa: SLF001
        if not sentinel_token:
            raise RuntimeError("sentinel check failed")

        return {
            "metadata": {
                "auth_device_id": did,
                "auth_sentinel_token": sentinel_token,
                "codexgen_flow": "authorize_continue",
            }
        }

    def run_submit_signup_email_step(self, *, did: str, sentinel_token: str) -> dict[str, Any]:
        result = self._engine._submit_auth_start(  # noqa: SLF001
            did,
            sentinel_token,
            screen_hint="signup",
            referer="https://auth.openai.com/create-account",
            log_label="提交注册表单(codexgen)",
            record_existing_account=True,
        )
        if not result.success:
            raise RuntimeError(f"submit signup email failed: {result.error_message}")
        return {"metadata": {"is_existing_account": self._engine._is_existing_account}}  # noqa: SLF001

    def run_register_password_step(self) -> dict[str, Any]:
        if self._engine._is_existing_account:  # noqa: SLF001
            return {"metadata": {"password_registration_skipped": True}}

        session = self._engine.session
        if session is None:
            raise RuntimeError("session missing for register step")

        password = self._engine._generate_password()  # noqa: SLF001
        self._engine.password = password
        body = json.dumps({"username": self._engine.email, "password": password})

        headers = {
            "referer": "https://auth.openai.com/create-account/password",
            "accept": "application/json",
            "content-type": "application/json",
        }
        sentinel = self._engine._check_sentinel(did=session.cookies.get("oai-did"))  # noqa: SLF001
        did = session.cookies.get("oai-did")
        if did and sentinel:
            headers["openai-sentinel-token"] = json.dumps(
                {
                    "p": "",
                    "t": "",
                    "c": sentinel,
                    "id": did,
                    "flow": "authorize_continue",
                }
            )

        response = session.post(
            OPENAI_API_ENDPOINTS["register"],
            headers=headers,
            data=body,
        )
        if response.status_code != 200:
            raise RuntimeError(f"register password failed: HTTP {response.status_code}")
        return {"password": password}

    def run_send_signup_otp_step(self) -> dict[str, Any]:
        if self._engine._is_existing_account:  # noqa: SLF001
            return {"metadata": {"send_signup_otp_skipped": True}}

        session = self._engine.session
        if session is None:
            raise RuntimeError("session missing for send otp step")
        self._engine._otp_sent_at = time.time()  # noqa: SLF001
        response = session.get(
            OPENAI_API_ENDPOINTS["send_otp"],
            headers={
                "referer": "https://auth.openai.com/create-account/password",
                "accept": "application/json",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"send signup otp failed: HTTP {response.status_code}")
        return {}

    def run_wait_signup_otp_step(self) -> dict[str, Any]:
        if self._engine._is_existing_account:  # noqa: SLF001
            return {"metadata": {"wait_signup_otp_skipped": True}}

        if not self._engine.email:
            raise RuntimeError("email missing for signup otp polling")
        email_id = (self._engine.email_info or {}).get("service_id")
        code = self._engine.email_service.get_verification_code(
            email=self._engine.email,
            email_id=email_id,
            timeout=120,
            otp_sent_at=self._engine._otp_sent_at,  # noqa: SLF001
        )
        if not code:
            raise RuntimeError("wait signup otp failed")
        self._signup_otp_code = code
        return {}

    def run_validate_signup_otp_step(self) -> dict[str, Any]:
        if self._engine._is_existing_account:  # noqa: SLF001
            return {"metadata": {"validate_signup_otp_skipped": True}}
        if not self._signup_otp_code:
            raise RuntimeError("signup otp missing")
        if not self._engine._validate_verification_code(self._signup_otp_code):  # noqa: SLF001
            raise RuntimeError("validate signup otp failed")
        return {}

    def run_create_account_profile_step(self) -> dict[str, Any]:
        try:
            return self._engine.run_create_account_profile_step()
        except RuntimeError as first_error:
            if self._run_create_account_fallback():
                return {"metadata": {"create_account_fallback_used": True}}
            raise first_error

    def _run_create_account_fallback(self) -> bool:
        """
        codexgen 风格 create_account fallback：
        - 优先尝试 oauth_create_account 风格 sentinel header
        - 再退化为普通 create_account 提交
        """
        session = self._engine.session
        if session is None:
            return False

        user_info = generate_random_user_info()
        body = json.dumps(user_info)
        headers = {
            "referer": "https://auth.openai.com/about-you",
            "accept": "application/json",
            "content-type": "application/json",
        }

        did = session.cookies.get("oai-did")
        sentinel_token = self._engine._check_sentinel(did) if did else None  # noqa: SLF001
        if did and sentinel_token:
            headers["openai-sentinel-token"] = json.dumps(
                {
                    "p": "",
                    "t": "",
                    "c": sentinel_token,
                    "id": did,
                    "flow": "oauth_create_account",
                }
            )

        try:
            response = session.post(
                OPENAI_API_ENDPOINTS["create_account"],
                headers=headers,
                data=body,
            )
            if response.status_code == 200:
                return True
        except Exception:
            pass

        try:
            response = session.post(
                OPENAI_API_ENDPOINTS["create_account"],
                headers={
                    "referer": "https://auth.openai.com/about-you",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=body,
            )
            return response.status_code == 200
        except Exception:
            return False

    def run_prepare_token_acquisition_step(self) -> dict[str, Any]:
        if self._engine._is_existing_account:  # noqa: SLF001
            self._engine._token_acquisition_requires_login = False  # noqa: SLF001
            return {"metadata": {"token_acquired_via_relogin": False}}

        self._engine._token_acquisition_requires_login = True  # noqa: SLF001
        self._engine._reset_auth_flow()  # noqa: SLF001
        did, sentinel_token = self._engine._prepare_authorize_flow("codexgen重新登录")  # noqa: SLF001
        if not did:
            raise RuntimeError("prepare relogin failed: missing device id")
        if not sentinel_token:
            raise RuntimeError("prepare relogin failed: missing sentinel token")
        return {
            "metadata": {
                "token_acquired_via_relogin": True,
                "relogin_device_id": did,
                "relogin_sentinel_token": sentinel_token,
            }
        }

    def run_submit_login_email_step(self, *, did: str, sentinel_token: str) -> dict[str, Any]:
        if self._engine._is_existing_account:  # noqa: SLF001
            return {"metadata": {"submit_login_email_skipped": True}}
        result = self._engine._submit_auth_start(  # noqa: SLF001
            did,
            sentinel_token,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            log_label="提交登录邮箱(codexgen)",
            record_existing_account=False,
        )
        if not result.success:
            raise RuntimeError(f"submit login email failed: {result.error_message}")
        if result.page_type != OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            raise RuntimeError(f"unexpected login page type: {result.page_type or 'unknown'}")
        return {}

    def run_submit_login_password_step(self) -> dict[str, Any]:
        if self._engine._is_existing_account:  # noqa: SLF001
            return {"metadata": {"submit_login_password_skipped": True}}

        result = self._engine._submit_login_password()  # noqa: SLF001
        if not result.success:
            raise RuntimeError(f"submit login password failed: {result.error_message}")
        if not result.is_existing_account:
            raise RuntimeError(f"unexpected page after login password: {result.page_type or 'unknown'}")
        return {}

    def run_wait_login_otp_step(self) -> dict[str, Any]:
        if not self._engine.email:
            raise RuntimeError("email missing for login otp polling")
        email_id = (self._engine.email_info or {}).get("service_id")
        code = self._engine.email_service.get_verification_code(
            email=self._engine.email,
            email_id=email_id,
            timeout=120,
            otp_sent_at=self._engine._otp_sent_at,  # noqa: SLF001
        )
        if not code:
            raise RuntimeError("wait login otp failed")
        self._login_otp_code = code
        return {}

    def run_validate_login_otp_step(self) -> dict[str, Any]:
        if not self._login_otp_code:
            raise RuntimeError("login otp missing")
        if not self._engine._validate_verification_code(self._login_otp_code):  # noqa: SLF001
            raise RuntimeError("validate login otp failed")
        return {}

    def run_resolve_consent_and_workspace_step(self) -> dict[str, Any]:
        workspace_id = self._engine._get_workspace_id()  # noqa: SLF001
        if not workspace_id:
            raise RuntimeError("workspace id missing")
        continue_url = self._engine._select_workspace(workspace_id)  # noqa: SLF001
        if not continue_url:
            raise RuntimeError("select workspace failed")
        callback_url = self._engine._follow_redirects(continue_url)  # noqa: SLF001
        if not callback_url:
            raise RuntimeError("oauth callback url missing")
        return {
            "metadata": {
                "workspace_id": workspace_id,
                "oauth_callback_url": callback_url,
                "codexgen_consent_chain": True,
            }
        }

    def run_exchange_oauth_token_step(self, *, callback_url: str | None) -> dict[str, Any]:
        if not callback_url:
            raise RuntimeError("oauth callback url missing")
        token_info = self._engine._handle_oauth_callback(callback_url)  # noqa: SLF001
        if not token_info:
            raise RuntimeError("exchange oauth token failed")

        session_cookie = self._engine.session.cookies.get("__Secure-next-auth.session-token") if self._engine.session else None
        return {
            "metadata": {
                "account_id": token_info.get("account_id"),
                "access_token": token_info.get("access_token"),
                "refresh_token": token_info.get("refresh_token"),
                "id_token": token_info.get("id_token"),
                "session_token": session_cookie,
                "token_acquired_via_relogin": self._engine._token_acquisition_requires_login,  # noqa: SLF001
                "codexgen_flow_page_type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"],
            }
        }


def build_codexgen_runtime(
    *,
    email_service: BaseEmailService,
    proxy_url: str | None = None,
    callback_logger: Callable[[str], None] | None = None,
    task_uuid: str | None = None,
) -> CodexgenPipelineRuntime:
    return CodexgenPipelineRuntime(
        email_service=email_service,
        proxy_url=proxy_url,
        callback_logger=callback_logger,
        task_uuid=task_uuid,
    )


def codexgen_check_ip_location_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_check_ip_location_step()


def codexgen_init_auth_session_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_init_auth_session_step()


def codexgen_prepare_authorize_flow_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_prepare_authorize_flow_step()


def codexgen_submit_signup_email_step(ctx: PipelineContext) -> dict[str, Any]:
    metadata = ctx.metadata or {}
    return get_registration_engine(ctx).run_submit_signup_email_step(
        did=metadata.get("auth_device_id"),
        sentinel_token=metadata.get("auth_sentinel_token"),
    )


def codexgen_register_password_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_register_password_step()


def codexgen_send_signup_otp_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_send_signup_otp_step()


def codexgen_validate_signup_otp_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_validate_signup_otp_step()


def codexgen_create_account_profile_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_create_account_profile_step()


def codexgen_prepare_token_acquisition_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_prepare_token_acquisition_step()


def codexgen_submit_login_email_step(ctx: PipelineContext) -> dict[str, Any]:
    metadata = ctx.metadata or {}
    return get_registration_engine(ctx).run_submit_login_email_step(
        did=metadata.get("relogin_device_id"),
        sentinel_token=metadata.get("relogin_sentinel_token"),
    )


def codexgen_submit_login_password_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_submit_login_password_step()


def codexgen_validate_login_otp_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_validate_login_otp_step()


def codexgen_resolve_consent_and_workspace_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_resolve_consent_and_workspace_step()


def build_codexgen_pipeline_definition() -> PipelineDefinition:
    return PipelineDefinition(
        pipeline_key="codexgen_pipeline",
        steps=[
            StepDefinition("get_proxy_ip", get_proxy_ip_step, impl_key="common.get_proxy_ip"),
            StepDefinition("check_ip_location", codexgen_check_ip_location_step, impl_key="codexgen.check_ip_location"),
            StepDefinition("create_email", create_email_step, impl_key="common.create_email"),
            StepDefinition("init_auth_session", codexgen_init_auth_session_step, impl_key="codexgen.init_auth_session"),
            StepDefinition("prepare_authorize_flow", codexgen_prepare_authorize_flow_step, impl_key="codexgen.prepare_authorize_flow"),
            StepDefinition("submit_signup_email", codexgen_submit_signup_email_step, impl_key="codexgen.submit_signup_email"),
            StepDefinition("register_password", codexgen_register_password_step, impl_key="codexgen.register_password"),
            StepDefinition("send_signup_otp", codexgen_send_signup_otp_step, impl_key="codexgen.send_signup_otp"),
            StepDefinition("wait_signup_otp", wait_signup_otp_step, impl_key="common.wait_signup_otp"),
            StepDefinition("validate_signup_otp", codexgen_validate_signup_otp_step, impl_key="codexgen.validate_signup_otp"),
            StepDefinition("create_account_profile", codexgen_create_account_profile_step, impl_key="codexgen.create_account_profile"),
            StepDefinition("prepare_token_acquisition", codexgen_prepare_token_acquisition_step, impl_key="codexgen.prepare_token_acquisition"),
            StepDefinition("submit_login_email", codexgen_submit_login_email_step, impl_key="codexgen.submit_login_email"),
            StepDefinition("submit_login_password", codexgen_submit_login_password_step, impl_key="codexgen.submit_login_password"),
            StepDefinition("wait_login_otp", wait_login_otp_step, impl_key="common.wait_login_otp"),
            StepDefinition("validate_login_otp", codexgen_validate_login_otp_step, impl_key="codexgen.validate_login_otp"),
            StepDefinition("resolve_consent_and_workspace", codexgen_resolve_consent_and_workspace_step, impl_key="codexgen.resolve_consent_and_workspace"),
            StepDefinition("exchange_oauth_token", exchange_oauth_token_step, impl_key="common.exchange_oauth_token"),
            StepDefinition("persist_account", persist_account_step, impl_key="common.persist_account"),
            StepDefinition("schedule_survival_checks", schedule_survival_checks_step, impl_key="common.schedule_survival_checks"),
        ],
    )


def register_codexgen_pipeline() -> PipelineDefinition:
    existing = PIPELINE_REGISTRY.get("codexgen_pipeline")
    if existing is not None:
        return existing

    definition = build_codexgen_pipeline_definition()
    try:
        register_pipeline(definition)
    except ValueError:
        return PIPELINE_REGISTRY["codexgen_pipeline"]
    return definition
