import pytest

from src.config.constants import EmailServiceType
from src.core.pipeline import PipelineContext, PipelineRunner
from src.core.pipeline.registry import PIPELINE_REGISTRY, get_pipeline
from src.core.pipeline.steps import common as common_steps
from src.core.registration_job import run_registration_job
from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager
from src.services.base import BaseEmailService


class FakeSharedEmailService(BaseEmailService):
    def __init__(self):
        super().__init__(EmailServiceType.TEMPMAIL)
        self.create_calls = 0
        self.otp_calls: list[dict] = []
        self._codes = ["123456", "654321"]

    def create_email(self, config=None):
        self.create_calls += 1
        return {
            "email": "tester@example.com",
            "service_id": "mailbox-1",
        }

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=r"(?<!\d)(\d{6})(?!\d)", otp_sent_at=None):
        self.otp_calls.append(
            {
                "email": email,
                "email_id": email_id,
                "otp_sent_at": otp_sent_at,
            }
        )
        return self._codes.pop(0) if self._codes else "111111"

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id):
        return True

    def check_health(self):
        return True


class FakeCodexgenRuntime:
    def __init__(self, email_service: FakeSharedEmailService):
        self.email_service = email_service
        self.email: str | None = None
        self.password = "CodexgenPass123!"
        self.email_info: dict = {}

    def run_check_ip_location_step(self):
        return {"metadata": {"ip_location": "US"}}

    def run_create_email_step(self):
        info = self.email_service.create_email()
        self.email = info["email"]
        self.email_info = info
        return {"email": self.email, "metadata": {"email_info": info}}

    def run_init_auth_session_step(self):
        return {}

    def run_prepare_authorize_flow_step(self):
        return {"metadata": {"auth_device_id": "did-1", "auth_sentinel_token": "sentinel-1"}}

    def run_submit_signup_email_step(self, *, did: str, sentinel_token: str):
        assert did == "did-1"
        assert sentinel_token == "sentinel-1"
        return {}

    def run_register_password_step(self):
        return {"password": self.password}

    def run_send_signup_otp_step(self):
        return {}

    def run_wait_signup_otp_step(self):
        self.email_service.get_verification_code(self.email, email_id=self.email_info.get("service_id"))
        return {}

    def run_validate_signup_otp_step(self):
        return {}

    def run_create_account_profile_step(self):
        return {}

    def run_prepare_token_acquisition_step(self):
        return {"metadata": {"token_acquired_via_relogin": True, "relogin_device_id": "did-1", "relogin_sentinel_token": "sentinel-1"}}

    def run_submit_login_email_step(self, *, did: str, sentinel_token: str):
        assert did == "did-1"
        assert sentinel_token == "sentinel-1"
        return {}

    def run_submit_login_password_step(self):
        return {}

    def run_wait_login_otp_step(self):
        self.email_service.get_verification_code(self.email, email_id=self.email_info.get("service_id"))
        return {}

    def run_validate_login_otp_step(self):
        return {}

    def run_resolve_consent_and_workspace_step(self):
        return {
            "metadata": {
                "workspace_id": "ws-1",
                "oauth_callback_url": "http://localhost:1455/auth/callback?code=code-1&state=state-1",
            }
        }

    def run_exchange_oauth_token_step(self, *, callback_url: str | None):
        assert callback_url and "code=" in callback_url
        return {
            "metadata": {
                "account_id": "acct-1",
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "id_token": "id-1",
                "session_token": "session-1",
            }
        }


@pytest.fixture
def temp_db(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path / 'codexgen-pipeline.db'}")
    Base.metadata.create_all(bind=manager.engine)
    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def clean_pipeline_registry():
    PIPELINE_REGISTRY.clear()
    try:
        yield
    finally:
        PIPELINE_REGISTRY.clear()


def test_get_codexgen_pipeline_is_available_with_common_step_bindings():
    pipeline = get_pipeline("codexgen_pipeline")
    assert pipeline is not None
    assert get_pipeline("codexgen_pipeline") is pipeline

    steps = {item.step_key: item for item in pipeline.steps}
    assert steps["create_email"].impl_key == "common.create_email"
    assert steps["create_email"].handler is common_steps.create_email_step
    assert steps["wait_signup_otp"].impl_key == "common.wait_signup_otp"
    assert steps["wait_signup_otp"].handler is common_steps.wait_signup_otp_step
    assert steps["wait_login_otp"].impl_key == "common.wait_login_otp"
    assert steps["wait_login_otp"].handler is common_steps.wait_login_otp_step
    assert steps["exchange_oauth_token"].impl_key == "common.exchange_oauth_token"
    assert steps["exchange_oauth_token"].handler is common_steps.exchange_oauth_token_step


def test_codexgen_pipeline_uses_shared_email_service(temp_db):
    pipeline = get_pipeline("codexgen_pipeline")
    assert pipeline is not None

    task_uuid = "task-codexgen-pipeline"
    crud.create_registration_task(temp_db, task_uuid=task_uuid)

    email_service = FakeSharedEmailService()
    runtime = FakeCodexgenRuntime(email_service)
    ctx = PipelineContext(
        task_uuid=task_uuid,
        pipeline_key="codexgen_pipeline",
        proxy_url="http://proxy-a",
        metadata={"registration_engine": runtime},
    )

    PipelineRunner(temp_db).run(pipeline, ctx)

    assert email_service.create_calls == 1
    assert len(email_service.otp_calls) == 2
    assert ctx.email and ctx.email.endswith("@example.com")
    assert ctx.metadata["token_acquired_via_relogin"] is True


def test_run_registration_job_dispatches_to_codexgen_pipeline(temp_db, monkeypatch):
    task_uuid = "task-codexgen-dispatch"
    crud.create_registration_task(temp_db, task_uuid=task_uuid)

    created: dict[str, object] = {}

    def fake_resolve_email_service(**kwargs):
        return EmailServiceType.TEMPMAIL, {}, None

    def fake_create_email_service(service_type, config):
        service = FakeSharedEmailService()
        created["service"] = service
        return service

    def fake_build_codexgen_runtime(*, email_service, proxy_url, callback_logger, task_uuid):
        runtime = FakeCodexgenRuntime(email_service)
        created["runtime"] = runtime
        return runtime

    monkeypatch.setattr("src.core.registration_job._resolve_email_service", fake_resolve_email_service)
    monkeypatch.setattr("src.core.registration_job.EmailServiceFactory.create", fake_create_email_service)
    monkeypatch.setattr("src.core.registration_job.build_codexgen_runtime", fake_build_codexgen_runtime)

    result = run_registration_job(
        db=temp_db,
        email_service_type="tempmail",
        email_service_id=None,
        proxy="http://proxy-a",
        email_service_config={},
        pipeline_key="codexgen_pipeline",
        task_uuid=task_uuid,
    )

    assert result.success is True
    assert result.email == "tester@example.com"
    assert result.account_id is not None
    assert created["service"].create_calls == 1
    assert len(created["service"].otp_calls) == 2
