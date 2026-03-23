import pytest

from src.core.pipeline.context import PipelineContext
from src.core.pipeline.registry import PIPELINE_REGISTRY, get_pipeline
from src.core.pipeline.runner import PipelineRunner
from src.core.pipeline.steps import common as common_steps
from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager


class FakeCurrentPipelineEngine:
    def __init__(self):
        self.calls: list[str] = []

    def run_check_ip_location_step(self):
        self.calls.append("run_check_ip_location_step")
        return {"metadata": {"ip_location": "US"}}

    def run_create_email_step(self):
        self.calls.append("run_create_email_step")
        return {"email": "tester@example.com", "metadata": {"email_info": {"service_id": "mailbox-1"}}}

    def run_init_auth_session_step(self):
        self.calls.append("run_init_auth_session_step")
        return {}

    def run_prepare_authorize_flow_step(self):
        self.calls.append("run_prepare_authorize_flow_step")
        return {"metadata": {"auth_device_id": "did-1", "auth_sentinel_token": "sentinel-1"}}

    def run_submit_signup_email_step(self, *, did: str, sentinel_token: str):
        self.calls.append("run_submit_signup_email_step")
        assert did == "did-1"
        assert sentinel_token == "sentinel-1"
        return {}

    def run_register_password_step(self):
        self.calls.append("run_register_password_step")
        return {"password": "StrongPass123!"}

    def run_send_signup_otp_step(self):
        self.calls.append("run_send_signup_otp_step")
        return {}

    def run_wait_signup_otp_step(self):
        self.calls.append("run_wait_signup_otp_step")
        return {}

    def run_validate_signup_otp_step(self):
        self.calls.append("run_validate_signup_otp_step")
        return {}

    def run_create_account_profile_step(self):
        self.calls.append("run_create_account_profile_step")
        return {}

    def run_prepare_token_acquisition_step(self):
        self.calls.append("run_prepare_token_acquisition_step")
        return {"metadata": {"token_acquired_via_relogin": True}}

    def run_submit_login_email_step(self):
        self.calls.append("run_submit_login_email_step")
        return {}

    def run_submit_login_password_step(self):
        self.calls.append("run_submit_login_password_step")
        return {}

    def run_wait_login_otp_step(self):
        self.calls.append("run_wait_login_otp_step")
        return {}

    def run_validate_login_otp_step(self):
        self.calls.append("run_validate_login_otp_step")
        return {}

    def run_resolve_consent_and_workspace_step(self):
        self.calls.append("run_resolve_consent_and_workspace_step")
        return {"metadata": {"workspace_id": "ws-1", "oauth_callback_url": "http://localhost:1455/auth/callback?code=c&state=s"}}

    def run_exchange_oauth_token_step(self, *, callback_url: str | None):
        self.calls.append("run_exchange_oauth_token_step")
        assert callback_url and "code=" in callback_url
        return {"metadata": {"token_acquired_via_relogin": True}}


@pytest.fixture
def fake_db(tmp_path):
    manager = DatabaseSessionManager(f"sqlite:///{tmp_path / 'current-pipeline.db'}")
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


def test_current_pipeline_runs_signup_then_relogin_steps(fake_db):
    pipeline = get_pipeline("current_pipeline")
    assert pipeline is not None

    task_uuid = "task-current-pipeline"
    crud.create_registration_task(fake_db, task_uuid=task_uuid)

    engine = FakeCurrentPipelineEngine()
    ctx = PipelineContext(
        task_uuid=task_uuid,
        pipeline_key="current_pipeline",
        metadata={
            "registration_engine": engine,
            "proxy_preflight_results": [
                {"proxy_id": 1, "proxy_url": "http://proxy-a", "status": "available"},
            ],
        },
    )

    PipelineRunner(fake_db).run(pipeline, ctx)

    assert ctx.proxy_url == "http://proxy-a"
    assert ctx.email == "tester@example.com"
    assert ctx.metadata["token_acquired_via_relogin"] is True
    assert engine.calls == [
        "run_check_ip_location_step",
        "run_create_email_step",
        "run_init_auth_session_step",
        "run_prepare_authorize_flow_step",
        "run_submit_signup_email_step",
        "run_register_password_step",
        "run_send_signup_otp_step",
        "run_wait_signup_otp_step",
        "run_validate_signup_otp_step",
        "run_create_account_profile_step",
        "run_prepare_token_acquisition_step",
        "run_submit_login_email_step",
        "run_submit_login_password_step",
        "run_wait_login_otp_step",
        "run_validate_login_otp_step",
        "run_resolve_consent_and_workspace_step",
        "run_exchange_oauth_token_step",
    ]


def test_current_pipeline_uses_common_step_bindings():
    pipeline = get_pipeline("current_pipeline")
    assert pipeline is not None
    steps = {item.step_key: item for item in pipeline.steps}

    assert steps["create_email"].impl_key == "common.create_email"
    assert steps["create_email"].handler is common_steps.create_email_step

    assert steps["wait_signup_otp"].impl_key == "common.wait_signup_otp"
    assert steps["wait_signup_otp"].handler is common_steps.wait_signup_otp_step

    assert steps["wait_login_otp"].impl_key == "common.wait_login_otp"
    assert steps["wait_login_otp"].handler is common_steps.wait_login_otp_step

    assert steps["exchange_oauth_token"].impl_key == "common.exchange_oauth_token"
    assert steps["exchange_oauth_token"].handler is common_steps.exchange_oauth_token_step
