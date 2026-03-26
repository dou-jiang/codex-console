from packages.registration_core.engine import RegistrationEngine
from packages.registration_core.models import RegistrationInput
from src.config.constants import EmailServiceType
from src.services.base import BaseEmailService


class FakeEmailService(BaseEmailService):
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


def test_engine_accepts_registration_input(monkeypatch):
    class FakeLegacyEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
            self.email_service = email_service
            self.proxy_url = proxy_url

        def run(self):
            class LegacyResult:
                success = True
                email = "tester@example.com"
                password = "secret"
                account_id = "acct-1"
                workspace_id = "ws-1"
                error_message = ""
                logs = ["step one", "step two"]

            return LegacyResult()

    monkeypatch.setattr("packages.registration_core.engine.LegacyRegistrationEngine", FakeLegacyEngine)

    engine = RegistrationEngine(FakeEmailService())
    result = engine.run(RegistrationInput(email_service_type="tempmail"))

    assert result.success is True
    assert result.identity.email == "tester@example.com"
    assert result.identity.account_id == "acct-1"
    assert result.identity.workspace_id == "ws-1"
    assert [entry.message for entry in result.logs] == ["step one", "step two"]
