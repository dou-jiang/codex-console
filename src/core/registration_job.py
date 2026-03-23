from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from ..config.constants import EmailServiceType
from ..config.settings import get_settings
from .pipeline import PipelineContext, PipelineRunner
from .pipeline.registry import get_pipeline
from .pipeline.steps.codexgen import build_codexgen_runtime
from .register import RegistrationEngine
from ..database import crud
from ..database.models import Account, EmailService
from ..services import EmailServiceFactory

logger = logging.getLogger(__name__)


@dataclass
class RegistrationJobResult:
    success: bool
    account_id: int | None = None
    email: str | None = None
    error_message: str | None = None
    email_service_id: int | None = None
    result_payload: dict[str, Any] | None = None


def _normalize_email_service_config(
    service_type: EmailServiceType,
    config: dict[str, Any] | None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """按服务类型兼容旧字段名，避免不同服务的配置键互相污染。"""
    normalized = config.copy() if config else {}

    if "api_url" in normalized and "base_url" not in normalized:
        normalized["base_url"] = normalized.pop("api_url")

    if service_type == EmailServiceType.MOE_MAIL:
        if "domain" in normalized and "default_domain" not in normalized:
            normalized["default_domain"] = normalized.pop("domain")
    elif service_type in (EmailServiceType.TEMP_MAIL, EmailServiceType.FREEMAIL):
        if "default_domain" in normalized and "domain" not in normalized:
            normalized["domain"] = normalized.pop("default_domain")
    elif service_type == EmailServiceType.DUCK_MAIL:
        if "domain" in normalized and "default_domain" not in normalized:
            normalized["default_domain"] = normalized.pop("domain")

    if proxy_url and "proxy_url" not in normalized:
        normalized["proxy_url"] = proxy_url

    return normalized


def _resolve_email_service(
    *,
    db,
    email_service_type: str,
    email_service_id: int | None,
    proxy: str | None,
    email_service_config: dict[str, Any] | None,
) -> tuple[EmailServiceType, dict[str, Any], int | None]:
    service_type = EmailServiceType(email_service_type)
    settings = get_settings()
    resolved_service_id: int | None = None

    if email_service_id:
        db_service = (
            db.query(EmailService)
            .filter(EmailService.id == email_service_id, EmailService.enabled == True)
            .first()
        )
        if not db_service:
            raise ValueError(f"邮箱服务不存在或已禁用: {email_service_id}")

        service_type = EmailServiceType(db_service.service_type)
        config = _normalize_email_service_config(service_type, db_service.config, proxy)
        resolved_service_id = db_service.id
        return service_type, config, resolved_service_id

    if service_type == EmailServiceType.TEMPMAIL:
        config = {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
            "proxy_url": proxy,
        }
        return service_type, config, resolved_service_id

    if service_type == EmailServiceType.MOE_MAIL:
        db_service = (
            db.query(EmailService)
            .filter(EmailService.service_type == "moe_mail", EmailService.enabled == True)
            .order_by(EmailService.priority.asc())
            .first()
        )
        if db_service and db_service.config:
            config = _normalize_email_service_config(service_type, db_service.config, proxy)
            resolved_service_id = db_service.id
            return service_type, config, resolved_service_id

        if settings.custom_domain_base_url and settings.custom_domain_api_key:
            config = {
                "base_url": settings.custom_domain_base_url,
                "api_key": settings.custom_domain_api_key.get_secret_value() if settings.custom_domain_api_key else "",
                "proxy_url": proxy,
            }
            return service_type, config, resolved_service_id

        raise ValueError("没有可用的自定义域名邮箱服务，请先在设置中配置")

    if service_type == EmailServiceType.OUTLOOK:
        outlook_services = (
            db.query(EmailService)
            .filter(EmailService.service_type == "outlook", EmailService.enabled == True)
            .order_by(EmailService.priority.asc())
            .all()
        )
        if not outlook_services:
            raise ValueError("没有可用的 Outlook 账户，请先在设置中导入账户")

        selected_service = None
        for service in outlook_services:
            email = service.config.get("email") if service.config else None
            if not email:
                continue
            existing = db.query(Account).filter(Account.email == email).first()
            if not existing:
                selected_service = service
                break

        if selected_service and selected_service.config:
            config = selected_service.config.copy()
            resolved_service_id = selected_service.id
            return service_type, config, resolved_service_id

        raise ValueError("所有 Outlook 账户都已注册过 OpenAI 账号，请添加新的 Outlook 账户")

    if service_type == EmailServiceType.DUCK_MAIL:
        db_service = (
            db.query(EmailService)
            .filter(EmailService.service_type == "duck_mail", EmailService.enabled == True)
            .order_by(EmailService.priority.asc())
            .first()
        )
        if not (db_service and db_service.config):
            raise ValueError("没有可用的 DuckMail 邮箱服务，请先在邮箱服务页面添加服务")

        config = _normalize_email_service_config(service_type, db_service.config, proxy)
        resolved_service_id = db_service.id
        return service_type, config, resolved_service_id

    if service_type == EmailServiceType.FREEMAIL:
        db_service = (
            db.query(EmailService)
            .filter(EmailService.service_type == "freemail", EmailService.enabled == True)
            .order_by(EmailService.priority.asc())
            .first()
        )
        if not (db_service and db_service.config):
            raise ValueError("没有可用的 Freemail 邮箱服务，请先在邮箱服务页面添加服务")

        config = _normalize_email_service_config(service_type, db_service.config, proxy)
        resolved_service_id = db_service.id
        return service_type, config, resolved_service_id

    if service_type == EmailServiceType.IMAP_MAIL:
        db_service = (
            db.query(EmailService)
            .filter(EmailService.service_type == "imap_mail", EmailService.enabled == True)
            .order_by(EmailService.priority.asc())
            .first()
        )
        if not (db_service and db_service.config):
            raise ValueError("没有可用的 IMAP 邮箱服务，请先在邮箱服务中添加")

        config = _normalize_email_service_config(service_type, db_service.config, proxy)
        resolved_service_id = db_service.id
        return service_type, config, resolved_service_id

    return service_type, (email_service_config or {}), resolved_service_id


def run_registration_job(
    *,
    db,
    email_service_type: str,
    email_service_id: int | None,
    proxy: str | None,
    email_service_config: dict[str, Any] | None,
    pipeline_key: str = "current_pipeline",
    auto_upload: bool = False,
    callback_logger: Callable[[str], None] | None = None,
    task_uuid: str | None = None,
) -> RegistrationJobResult:
    """执行单账号注册并落库，返回统一结果供路由和定时任务复用。"""
    known_email: str | None = None
    known_service_id: int | None = email_service_id
    known_result_payload: dict[str, Any] | None = None
    try:
        service_type, config, resolved_service_id = _resolve_email_service(
            db=db,
            email_service_type=email_service_type,
            email_service_id=email_service_id,
            proxy=proxy,
            email_service_config=email_service_config,
        )
        known_service_id = resolved_service_id if resolved_service_id is not None else known_service_id

        email_service = EmailServiceFactory.create(service_type, config)
        if pipeline_key == "codexgen_pipeline":
            account, result_payload = _run_pipeline_registration(
                db=db,
                pipeline_key=pipeline_key,
                email_service=email_service,
                proxy=proxy,
                callback_logger=callback_logger,
                task_uuid=task_uuid,
                resolved_service_id=resolved_service_id,
            )
            if not account:
                return RegistrationJobResult(
                    success=False,
                    email=(result_payload or {}).get("email"),
                    error_message=(result_payload or {}).get("error_message") or "注册失败",
                    email_service_id=resolved_service_id,
                    result_payload=result_payload,
                )
        else:
            engine = RegistrationEngine(
                email_service=email_service,
                proxy_url=proxy,
                callback_logger=callback_logger,
                task_uuid=task_uuid,
            )
            result = engine.run()
            result_payload = result.to_dict()
            known_email = result.email or known_email
            known_result_payload = result_payload

            if not result.success:
                return RegistrationJobResult(
                    success=False,
                    email=result.email or None,
                    error_message=result.error_message or "注册失败",
                    email_service_id=resolved_service_id,
                    result_payload=result_payload,
                )

            if not engine.save_to_database(result):
                return RegistrationJobResult(
                    success=False,
                    email=result.email or None,
                    error_message="保存注册账号到数据库失败",
                    email_service_id=resolved_service_id,
                    result_payload=result_payload,
                )

            db.expire_all()
            account = db.query(Account).filter(Account.email == result.email).first()
            if not account:
                return RegistrationJobResult(
                    success=False,
                    email=result.email or None,
                    error_message="注册成功但未找到已保存账号",
                    email_service_id=resolved_service_id,
                    result_payload=result_payload,
                )

        if auto_upload:
            logger.info("auto_upload is enabled but handled by caller")

        return RegistrationJobResult(
            success=True,
            account_id=account.id,
            email=account.email,
            email_service_id=resolved_service_id,
            result_payload=result_payload,
        )
    except Exception as exc:
        logger.error("run_registration_job failed: %s", exc)
        return RegistrationJobResult(
            success=False,
            email=known_email,
            email_service_id=known_service_id,
            error_message=str(exc) or "注册异常",
            result_payload=known_result_payload,
        )


def _run_pipeline_registration(
    *,
    db,
    pipeline_key: str,
    email_service,
    proxy: str | None,
    callback_logger: Callable[[str], None] | None,
    task_uuid: str | None,
    resolved_service_id: int | None,
) -> tuple[Account | None, dict[str, Any] | None]:
    pipeline = get_pipeline(pipeline_key)
    if not pipeline:
        raise RuntimeError(f"pipeline not found: {pipeline_key}")

    runtime = build_codexgen_runtime(
        email_service=email_service,
        proxy_url=proxy,
        callback_logger=callback_logger,
        task_uuid=task_uuid,
    )
    effective_task_uuid = task_uuid or f"pipeline-{uuid.uuid4()}"
    ctx = PipelineContext(
        task_uuid=effective_task_uuid,
        pipeline_key=pipeline_key,
        proxy_url=proxy,
        metadata={"registration_engine": runtime},
    )

    PipelineRunner(db).run(pipeline, ctx)
    result_payload = _build_pipeline_result_payload(ctx)
    account = _persist_pipeline_account(
        db=db,
        result_payload=result_payload,
        email_service=email_service,
        email_service_id=resolved_service_id,
        proxy=proxy,
    )
    return account, result_payload


def _build_pipeline_result_payload(ctx: PipelineContext) -> dict[str, Any]:
    metadata = _sanitize_metadata(ctx.metadata or {})
    return {
        "success": True,
        "email": ctx.email,
        "password": ctx.password or "",
        "account_id": metadata.get("account_id", ""),
        "workspace_id": metadata.get("workspace_id", ""),
        "access_token": metadata.get("access_token", ""),
        "refresh_token": metadata.get("refresh_token", ""),
        "id_token": metadata.get("id_token", ""),
        "session_token": metadata.get("session_token", ""),
        "error_message": "",
        "logs": [],
        "metadata": metadata,
        "source": "login" if metadata.get("is_existing_account") else "register",
    }


def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in metadata.items():
        if key == "registration_engine":
            continue
        sanitized[key] = _to_jsonable(value)
    return sanitized


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return str(value)


def _persist_pipeline_account(
    *,
    db,
    result_payload: dict[str, Any],
    email_service,
    email_service_id: int | None,
    proxy: str | None,
) -> Account | None:
    email = str(result_payload.get("email") or "").strip()
    if not email:
        return None

    settings = get_settings()
    existing = db.query(Account).filter(Account.email == email).first()
    if existing:
        return existing

    account = crud.create_account(
        db,
        email=email,
        password=result_payload.get("password") or "",
        client_id=settings.openai_client_id,
        session_token=result_payload.get("session_token") or None,
        email_service=email_service.service_type.value,
        email_service_id=str(email_service_id) if email_service_id is not None else None,
        account_id=result_payload.get("account_id") or None,
        workspace_id=result_payload.get("workspace_id") or None,
        access_token=result_payload.get("access_token") or None,
        refresh_token=result_payload.get("refresh_token") or None,
        id_token=result_payload.get("id_token") or None,
        proxy_used=proxy,
        extra_data=result_payload.get("metadata") or {},
        source=result_payload.get("source") or "register",
    )
    return account
