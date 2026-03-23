from __future__ import annotations

from typing import Any

from src.core.pipeline.context import PipelineContext
from src.core.pipeline.definitions import PipelineDefinition, StepDefinition
from src.core.pipeline.registry import PIPELINE_REGISTRY, register_pipeline

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


def current_check_ip_location_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_check_ip_location_step()


def current_init_auth_session_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_init_auth_session_step()


def current_prepare_authorize_flow_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_prepare_authorize_flow_step()


def current_submit_signup_email_step(ctx: PipelineContext) -> dict[str, Any]:
    metadata = ctx.metadata or {}
    did = metadata.get("auth_device_id")
    sentinel_token = metadata.get("auth_sentinel_token")
    return get_registration_engine(ctx).run_submit_signup_email_step(
        did=did,
        sentinel_token=sentinel_token,
    )


def current_register_password_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_register_password_step()


def current_send_signup_otp_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_send_signup_otp_step()


def current_validate_signup_otp_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_validate_signup_otp_step()


def current_create_account_profile_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_create_account_profile_step()


def current_prepare_token_acquisition_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_prepare_token_acquisition_step()


def current_submit_login_email_step(ctx: PipelineContext) -> dict[str, Any]:
    metadata = ctx.metadata or {}
    engine = get_registration_engine(ctx)
    try:
        return engine.run_submit_login_email_step(
            did=metadata.get("relogin_device_id"),
            sentinel_token=metadata.get("relogin_sentinel_token"),
        )
    except TypeError:
        # Keep compatibility with simple test doubles that expose a no-arg method.
        return engine.run_submit_login_email_step()


def current_submit_login_password_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_submit_login_password_step()


def current_validate_login_otp_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_validate_login_otp_step()


def current_resolve_consent_and_workspace_step(ctx: PipelineContext) -> dict[str, Any]:
    return get_registration_engine(ctx).run_resolve_consent_and_workspace_step()


def build_current_pipeline_definition() -> PipelineDefinition:
    return PipelineDefinition(
        pipeline_key="current_pipeline",
        steps=[
            StepDefinition("get_proxy_ip", get_proxy_ip_step, impl_key="common.get_proxy_ip"),
            StepDefinition("check_ip_location", current_check_ip_location_step, impl_key="current.check_ip_location"),
            StepDefinition("create_email", create_email_step, impl_key="common.create_email"),
            StepDefinition("init_auth_session", current_init_auth_session_step, impl_key="current.init_auth_session"),
            StepDefinition("prepare_authorize_flow", current_prepare_authorize_flow_step, impl_key="current.prepare_authorize_flow"),
            StepDefinition("submit_signup_email", current_submit_signup_email_step, impl_key="current.submit_signup_email"),
            StepDefinition("register_password", current_register_password_step, impl_key="current.register_password"),
            StepDefinition("send_signup_otp", current_send_signup_otp_step, impl_key="current.send_signup_otp"),
            StepDefinition("wait_signup_otp", wait_signup_otp_step, impl_key="common.wait_signup_otp"),
            StepDefinition("validate_signup_otp", current_validate_signup_otp_step, impl_key="current.validate_signup_otp"),
            StepDefinition("create_account_profile", current_create_account_profile_step, impl_key="current.create_account_profile"),
            StepDefinition("prepare_token_acquisition", current_prepare_token_acquisition_step, impl_key="current.prepare_token_acquisition"),
            StepDefinition("submit_login_email", current_submit_login_email_step, impl_key="current.submit_login_email"),
            StepDefinition("submit_login_password", current_submit_login_password_step, impl_key="current.submit_login_password"),
            StepDefinition("wait_login_otp", wait_login_otp_step, impl_key="common.wait_login_otp"),
            StepDefinition("validate_login_otp", current_validate_login_otp_step, impl_key="current.validate_login_otp"),
            StepDefinition("resolve_consent_and_workspace", current_resolve_consent_and_workspace_step, impl_key="current.resolve_consent_and_workspace"),
            StepDefinition("exchange_oauth_token", exchange_oauth_token_step, impl_key="common.exchange_oauth_token"),
            StepDefinition("persist_account", persist_account_step, impl_key="common.persist_account"),
            StepDefinition("schedule_survival_checks", schedule_survival_checks_step, impl_key="common.schedule_survival_checks"),
        ],
    )


def register_current_pipeline() -> PipelineDefinition:
    existing = PIPELINE_REGISTRY.get("current_pipeline")
    if existing is not None:
        return existing
    definition = build_current_pipeline_definition()
    try:
        register_pipeline(definition)
    except ValueError:
        return PIPELINE_REGISTRY["current_pipeline"]
    return definition
