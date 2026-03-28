import base64
import json

from src.config.constants import EmailServiceType
from src.core.openai.oauth import OAuthManager, submit_callback_url
from src.core.register import RegistrationEngine, RegistrationResult
from src.services.base import BaseEmailService


def _jwt(payload):
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode("utf-8")).decode("ascii").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{header}.{body}.sig"


class DummyEmailService(BaseEmailService):
    def __init__(self):
        super().__init__(EmailServiceType.TEMPMAIL)

    def create_email(self, config=None):
        return {"email": "tester@example.com", "service_id": "mailbox-1"}

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=r"(?<!\d)(\d{6})(?!\d)", otp_sent_at=None):
        return "123456"

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id):
        return True

    def check_health(self):
        return True


def test_extract_account_info_repairs_workspace_id_with_organization_claim():
    id_token = _jwt(
        {
            "email": "tester@example.com",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-123",
                "workspace_id": "acct-123",
                "organizations": [{"id": "org-real-123"}],
            },
        }
    )

    info = OAuthManager().extract_account_info(id_token)

    assert info["account_id"] == "acct-123"
    assert info["organization_id"] == "org-real-123"
    assert info["workspace_id"] == "org-real-123"


def test_submit_callback_url_returns_organization_and_normalized_workspace(monkeypatch):
    id_token = _jwt(
        {
            "email": "tester@example.com",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-234",
                "workspace_id": "acct-234",
                "organizations": [{"id": "org-real-234"}],
            },
        }
    )

    monkeypatch.setattr(
        "src.core.openai.oauth._post_form",
        lambda *_args, **_kwargs: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": id_token,
            "expires_in": 3600,
        },
    )

    result = json.loads(
        submit_callback_url(
            callback_url="http://localhost:1455/auth/callback?code=code-1&state=state-1",
            expected_state="state-1",
            code_verifier="verifier-1",
        )
    )

    assert result["account_id"] == "acct-234"
    assert result["organization_id"] == "org-real-234"
    assert result["workspace_id"] == "org-real-234"


def test_registration_engine_applies_workspace_id_from_oauth_callback():
    engine = RegistrationEngine(DummyEmailService())
    result = RegistrationResult(success=False, workspace_id="", logs=[])

    engine._apply_oauth_token_info(
        result,
        {
            "account_id": "acct-345",
            "workspace_id": "org-real-345",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
        },
    )

    assert result.account_id == "acct-345"
    assert result.workspace_id == "org-real-345"
    assert result.access_token == "access-token"
    assert result.refresh_token == "refresh-token"
